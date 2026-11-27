# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec. A spec file is required (instead of CLI flags)
# because the gi hook collects GTK 3 by default: hooksconfig is the only
# way to make it bundle the GTK 4 typelibs and libraries.
import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

WIN = sys.platform == 'win32'
ICON = 'assets/icon.ico' if WIN else None

# pypdfium2 (the PDFium bindings, pure Python + one shared library loaded with
# ctypes). On Windows the build runs under MSYS2, whose pip cannot install the
# official win_amd64 wheel into the environment: CI unpacks it into vendor/
# instead (see .github/workflows/build.yml).
VENDOR = os.path.join(SPECPATH, 'vendor')
pathex = [os.path.join(SPECPATH, 'src')]       # the package
if os.path.isdir(VENDOR):
    sys.path.insert(0, VENDOR)
    pathex.append(VENDOR)

# Application icons for the GTK icon theme (window icon, About dialog).
# PyInstaller's GLib runtime hook points XDG_DATA_DIRS at <bundle>/share.
datas = [('data/icons', 'share/icons')]
binaries = []

# The program's own licence (GPL-3.0-or-later) goes with every build, in
# _internal/licenses: the installer, the portable zip and the tar.gz.
datas.append((os.path.join(SPECPATH, 'LICENSE'), 'licenses'))

# PDFium itself, the version files pypdfium2 reads at import time, and its
# licence texts (dist-info).
binaries += collect_dynamic_libs('pypdfium2_raw')
datas += collect_data_files('pypdfium2_raw') + collect_data_files('pypdfium2')
datas += copy_metadata('pypdfium2')
if not any(os.path.basename(src).lower().startswith(('pdfium', 'libpdfium')) for src, _dest in binaries):
    raise SystemExit('pdfium shared library not found: is pypdfium2 installed (or unpacked in vendor/)?')

# Translations, compiled from po/ by `python tools/compile_po.py` (CI does it
# before running PyInstaller).
LOCALE_DIR = os.path.join(SPECPATH, 'locale')
if os.path.isdir(LOCALE_DIR):
    datas.append((LOCALE_DIR, 'share/locale'))
else:
    print('WARNING: locale/ missing: run tools/compile_po.py first (UI will be English only)')

if WIN:
    prefix = Path(sys.prefix)  # MSYS2: C:/msys64/ucrt64 (the one with libadwaita)

    # gdbus.exe lets GLib autolaunch a session bus on Windows, which
    # GApplication needs for its single-instance behaviour (a second launch
    # forwards its files to the running window). GLib looks for it next to
    # libgio-2.0-0.dll, i.e. in the bundle's _internal directory.
    gdbus = shutil.which('gdbus') or (
        str(prefix / 'bin' / 'gdbus.exe') if (prefix / 'bin' / 'gdbus.exe').is_file() else None)
    if gdbus:
        binaries.append((gdbus, '.'))
    else:
        print('WARNING: gdbus.exe not found: every launch will open a new window')

    # CA bundle for the update check: MSYS2's OpenSSL does not necessarily
    # read the Windows certificate store (see _ssl_context()).
    import ssl
    candidates = [
        prefix / 'etc' / 'ssl' / 'certs' / 'ca-bundle.crt',
        prefix / 'etc' / 'ssl' / 'cert.pem',
        Path(ssl.get_default_verify_paths().openssl_cafile or ''),
    ]
    for ca in candidates:
        if ca.is_file():
            datas.append((str(ca), 'certs'))
            if ca.name != 'ca-bundle.crt':
                print(f'NOTE: bundling {ca} as certs/{ca.name}; expected name is ca-bundle.crt')
            break
    else:
        print('WARNING: no CA bundle found: the update check may fail on TLS')

    # Two GPL-2.0 libraries come in with GTK's own dependencies: JBIG-KIT
    # (libjbig-0.dll, through libtiff) and LZO (liblzo2-2.dll, through
    # cairo). Their licence texts, as MSYS2 installs them, go with the build.
    for package, text in (('jbigkit', 'COPYING'), ('lzo2', 'LICENSE')):
        source = prefix / 'share' / 'licenses' / package / text
        if source.is_file():
            datas.append((str(source), f'licenses/{package}'))
        else:
            print(f'WARNING: {source} missing: the {package} licence is not bundled')

# Introspection modules the gi hook must collect: a libadwaita app on both
# systems (on Windows inside a window with the system title bar).
GI_VERSIONS = {'Gtk': '4.0', 'Gdk': '4.0', 'Adw': '1'}

a = Analysis(
    [os.path.join(SPECPATH, 'build-aux', 'windows', 'pdf_cropper_main.py')],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=['pypdfium2'],  # imported lazily, inside load_engine()
    hooksconfig={'gi': {'module-versions': GI_VERSIONS}},
)

if WIN:
    root = Path(SPECPATH)
    sys.path.insert(0, str(root / 'tools'))
    from package_inventory import inventory

    # MSYS2's icon caches come from a pacman hook, not from a package: the
    # workflow writes fresh ones for the slimmed themes and the seal records them.
    a.datas = [row for row in a.datas if Path(row[0]).name != 'icon-theme.cache']
    pure = [row for row in a.pure if row[1] != '-']
    _, notices = inventory([*a.binaries, *a.datas, *pure, *a.scripts],
                           prefix.parent, root, Path(workpath),
                           revision=os.environ.get('GITHUB_SHA', 'dev'))
    a.datas += notices

pyz = PYZ(a.pure)

# Windows: embed our own manifest, which declares system DPI awareness on
# top of PyInstaller's defaults, from the first instruction: GTK cannot
# switch the process to per-monitor awareness, where it would keep to whole
# scales (see the manifest).
MANIFEST = os.path.join(SPECPATH, 'build-aux', 'windows', 'pdf-cropper.manifest') if WIN else None

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='pdf-cropper',
    console=False,
    icon=ICON,
    manifest=MANIFEST,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name='pdf-cropper',
)
