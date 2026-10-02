<p align="center">
  <img src="assets/icon.png" alt="PDF Cropper" width="128" height="128">
</p>

<h1 align="center">PDF Cropper</h1>

<p align="center">
  Crop the white margins of PDF pages, many files at once.
</p>

<p align="center">
  <a href="https://github.com/daniel-g-carrasco/pdf-cropper/actions/workflows/build.yml">
    <img src="https://github.com/daniel-g-carrasco/pdf-cropper/actions/workflows/build.yml/badge.svg" alt="Build">
  </a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
</p>

Drawings often reach you plotted on a sheet much larger than the drawing
itself: an A0 page with the plan sitting in the middle of a sea of white.
PDF Cropper shrinks every page around what is actually drawn on it, leaving
a small margin, so the file opens straight on the content and prints without
wasting paper.

Drag & drop GUI (GTK 4 / libadwaita) + CLI, installable or portable on
**Windows**, Flatpak or portable on **Linux**. The cropped copy is saved next
to the original as `<name>_cropped.pdf`; **the original is never modified**,
and the content is not re-encoded: vectors stay vectors, text stays text.

## Download

Grab a build from the
[**Releases**](https://github.com/daniel-g-carrasco/pdf-cropper/releases) page:

| Platform | File | Notes |
|---|---|---|
| Windows installer | `pdf-cropper-setup-*-windows-x64.exe` | Start menu entry, uninstaller, Explorer context menu (optional), built-in update check |
| Windows portable | `pdf-cropper-*-windows-x64-portable.zip` | unzip anywhere, run `pdf-cropper.exe` — no installation, no admin rights; the Explorer entry can be added from *Preferenze* |
| Linux Flatpak | `pdf-cropper-*-linux-x64.flatpak` | `flatpak install pdf-cropper-*.flatpak` (the GNOME runtime comes from Flathub) |
| Linux portable | `pdf-cropper-*-linux-x64-portable.tar.gz` | untar, run `./pdf-cropper` |

Everything (GTK and the PDF engine included) ships inside the packages. The
installer defaults to a per-user install, so no administrator rights are
needed there either.

## Run from source

Python ≥ 3.9, [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) and,
for the GUI, PyGObject/GTK 4 with libadwaita 1.5 or newer. libadwaita is
optional: without it the GUI falls back to plain GTK 4.

```bash
# Debian/Ubuntu          # Fedora                          # Arch
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
                         sudo dnf install python3-gobject gtk4 libadwaita
                                                           sudo pacman -S python-gobject gtk4 libadwaita
pip install -r requirements.txt   # pypdfium2
python3 pdf_cropper.py
```

On Windows, use a release build. The command line needs nothing but
`pip install pypdfium2`; the GUI needs GTK 4 from MSYS2
(`pacman -S mingw-w64-x86_64-gtk4 mingw-w64-x86_64-python-gobject`).

## CLI

The same executable works headless when given arguments (the CLI needs no
GTK):

```bash
pdf-cropper drawing.pdf                     # single file -> drawing_cropped.pdf
pdf-cropper --overwrite projects/           # whole folder, recursive
pdf-cropper --margin 5 a.pdf b.pdf c.pdf    # batch, 5 mm left around the content
pdf-cropper --threshold 200 scan.pdf        # scans: ignore a greyish background
pdf-cropper --check-update                  # print the latest release
pdf-cropper --register | --unregister       # Windows: Explorer context menu
```

Exit code is non-zero if any file failed. Existing outputs are skipped unless
`--overwrite` is given; files with nothing to crop are skipped too.

| Option | Default | Meaning |
|---|---|---|
| `--margin MM` | `2` | white border left around the content, in millimetres (0–50) |
| `--threshold N` | `250` | grey level (1–255) below which a pixel counts as content |
| `--overwrite` | off | replace an existing `<name>_cropped.pdf` |

## Features

- **Drag & drop** files *or folders* (folders are scanned recursively; the
  `*_cropped.pdf` copies found there are left alone, so a folder can be
  dropped again after adding new files)
- **Batch**: hundreds of files in one go, every file listed at once with its
  state (queued, cropping with progress, done with the new page size);
  double-click a finished row to open the document, or use its folder button
  to reveal it
- **Single window**: opening more files (context menu, several files
  selected at once) adds them to the window already open
- **Adjustable margin**, remembered between sessions
- **Every page on its own**: each page of a document is cropped around its
  own content; blank pages are left as they are
- **Rotated pages**, existing crop boxes and pages whose origin is not at
  (0, 0) are handled
- Output is written next to the source file, never modifying the original
- **Light / dark theme** following the system (Windows personalization
  setting, freedesktop settings portal on Linux), or forced from *Preferences*
- **English and Italian** UI, following the desktop language (or forced from
  *Preferences*); the Windows installer is bilingual too and hands its
  language choice over to the app

### Windows integration

- **Context menu**: right-click one or more PDF files →
  *Ritaglia i bordi bianchi con PDF Cropper*. On Windows 11 the entry lives
  under *Mostra altre opzioni* (Shift+F10), like every classic shell verb.
  The installer offers it as an option; the portable build adds or removes it
  from *Preferenze*. Nothing else is registered: PDF Cropper never asks to
  become the default app for PDF files.
- The main menu opens with F10 (GTK convention) or a tap on Alt (Windows
  habit); another tap on Alt closes it.
- **Updates**: menu → *Controlla aggiornamenti…*, plus an automatic daily
  check (can be turned off in *Preferenze*). Installed builds download the
  new installer and launch it; the portable build is sent to the release page.
- Everything above is per-user (`HKEY_CURRENT_USER`) for the portable build;
  *Preferenze* → *Rimuovi* takes it away, as does the uninstaller.
- **Native window decorations** by default: the system title bar (dark when
  the theme is dark) handles moving, snapping and restoring from maximized,
  which GTK's own decorations get wrong on Windows. *Preferenze* switches
  back to GTK's header bar.

### Linux

- A libadwaita application: current GNOME look, `AdwStyleManager` follows the
  system light/dark preference on its own, `AdwAboutDialog` and
  `AdwPreferencesDialog` for the secondary windows.
- Registers for `application/pdf` through its desktop entry, so it shows up
  in *Open With* on PDF files in the file manager.
- No self-updater: updates come from Flatpak / the distribution, and the
  release notes are shipped as AppStream metadata for GNOME Software.

## How it works

1. Each page is rendered off-screen in greyscale with
   [PDFium](https://pdfium.googlesource.com/pdfium/) (the PDF engine of
   Chromium), at up to 16 megapixels per page.
2. The bounding box of every pixel that is not white is measured. Looking at
   the rendered page, rather than at the objects in the file, is what makes
   this robust: white fills, clipped-away geometry and objects parked outside
   the page do not count; yellow or light grey lines do.
3. The page boxes (`MediaBox`, `CropBox`, plus `TrimBox`/`BleedBox`/`ArtBox`
   where present) are set to that rectangle plus the margin, never larger
   than the original page. The page content streams are not touched.

On real A0/A1 drawings exported by AutoCAD the result matches Ghostscript's
`bbox` device within one or two points, in about 0.2 s per sheet.

Good to know:

- A title block or a frame drawn at the edge of the sheet is content: the
  page is cropped around it, which may mean not at all.
- Scans have no real white: paper grain and JPEG noise count as content at
  the default threshold. Lower it from the command line (`--threshold 200`).
- Cropping changes what viewers show and printers print. The geometry that
  fell outside is still in the file: this is not a tool for redacting.
- Password-protected PDFs are reported as errors.

## Flatpak

To build and install locally:

```bash
flatpak install flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak-builder --user --install --force-clean build-dir \
    build-aux/flatpak/io.github.daniel_g_carrasco.pdf-cropper.yaml
flatpak run io.github.daniel_g_carrasco.pdf-cropper
```

The manifest ([build-aux/flatpak/](build-aux/flatpak/)) installs pypdfium2
from its official wheel, then the script, the desktop entry, the AppStream
metainfo and the icons from [data/](data/). CI builds a `.flatpak` bundle
for every push and attaches it to releases. The app needs `--filesystem=host`
because the cropped file is written next to the original, wherever that is.

## Development

```bash
python tests/test_crop.py        # self-contained test suite (needs pypdfium2)
python tools/compile_po.py       # compile po/*.po into locale/ (needed to see translations)
python tools/make_icon.py        # regenerate assets/icon.*, data/icons PNGs, installer bitmaps (Pillow)
desktop-file-validate data/io.github.daniel_g_carrasco.pdf-cropper.desktop
appstreamcli validate --no-net data/io.github.daniel_g_carrasco.pdf-cropper.metainfo.xml
```

Builds are produced by [CI](.github/workflows/build.yml) (PyInstaller;
MSYS2 on Windows; installer compiled with Inno Setup; Flatpak via
flatpak-builder). Besides the test suite, CI starts the bundled GUI on
Windows and under Xvfb on Linux and checks that a file handed to it really
gets cropped. Tagging `v*` publishes a release; the tag must match
`__version__` in `pdf_cropper.py`, and the release should be listed in the
metainfo. The pypdfium2 version is pinned in the workflow
(`PYPDFIUM2_VERSION`) and in the Flatpak manifest (wheel URLs and checksums):
change both together.

Preferences are stored in `%LOCALAPPDATA%\pdf-cropper\settings.ini` on
Windows and `$XDG_CONFIG_HOME/pdf-cropper/settings.ini` on Linux.

Translations live in `po/` (English source strings in the code, one `.po`
per language). To refresh the template after changing strings:
`xgettext --from-code=UTF-8 --keyword=_ -o po/pdf-cropper.pot pdf_cropper.py`,
then merge with `msgmerge -U po/it.po po/pdf-cropper.pot`. The test suite
fails when a string of the source is missing from the catalogue.
Set `PDF_CROPPER_STARTUP_LOG=<file>` to get start-up timestamps appended to a
file (profiling aid), `PDF_CROPPER_NO_ADW=1` to force the plain GTK interface
on Linux.

## License

[MIT](LICENSE) © Daniel Grasso

The packaged builds include [PDFium](https://pdfium.googlesource.com/pdfium/)
and [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (BSD-3-Clause /
Apache-2.0, plus the licences of PDFium's own dependencies), and GTK with its
libraries (LGPL). Their licence texts ship inside the packages.
