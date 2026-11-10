"""Windows: the context-menu entry on PDF files, in the per-user registry."""

from __future__ import annotations

import sys

from pdf_cropper import PACKAGE_DIR, WIN_VERB
from pdf_cropper.i18n import _

from .updates import is_frozen

# One context-menu entry on PDF files. Unlike a viewer, this tool has no
# business being the default app for .pdf: nothing else is registered.
# The installer writes the same keys (HKLM when installed for all users);
# these functions cover the portable build.
_VERB_KEY = rf"Software\Classes\SystemFileAssociations\.pdf\shell\{WIN_VERB}"


def _launch_command() -> str:
    """Command line that opens files with this program (registry format)."""
    if is_frozen():
        return f'"{sys.executable}" --gui "%1"'
    launcher = PACKAGE_DIR.parent.parent / "bin" / "pdf-cropper"       # a run from the sources
    return f'"{sys.executable}" "{launcher}" --gui "%1"'


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
