"""Windows through ctypes, without the toolkit: the window brought to the
front, the executable's icon on the windows, the title bars dark or light.
"""

from __future__ import annotations

import os
import sys


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


_WIN_ICONS: dict = {}  # kept for the life of the process: a window does not copy its icon


def win_window_icons() -> None:
    """Give this process's windows the exe's own icon at the sizes Windows
    draws for each window's DPI (the title bar's small one, Alt+Tab's big
    one). GTK hands Windows a 48-pixel icon, which the title bar shrinks
    badly to 16; the exe's .ico holds 16, 20, 24 ... drawn for each size.
    Only for the built app: a run from the sources has no icon of its own."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return
    exe = os.path.normpath(sys.executable)
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    pid = os.getpid()

    def icon(size):
        if size not in _WIN_ICONS:
            handles, ids = (wintypes.HICON * 1)(), (wintypes.UINT * 1)()
            got = user32.PrivateExtractIconsW(exe, 0, size, size, handles, ids, 1, 0)
            _WIN_ICONS[size] = (handles[0] or 0) if got == 1 else 0
        return _WIN_ICONS[size]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            dpi = user32.GetDpiForWindow(hwnd) or 96
            for kind, metric in ((0, 49), (1, 11)):  # ICON_SMALL/SM_CXSMICON, ICON_BIG/SM_CXICON
                handle = icon(user32.GetSystemMetricsForDpi(metric, dpi))
                if handle:
                    user32.SendMessageW(hwnd, 0x0080, kind, handle)  # WM_SETICON
        return 1

    user32.EnumWindows(visit, 0)


def win_dark_titlebars(dark: bool, background: tuple[int, int, int] | None = None,
                       text: tuple[int, int, int] | None = None) -> None:
    """Ask DWM to paint the native title bars of this process as the content
    is: dark or light (Windows 10 1809+; the buttons' glyphs follow it), and
    on Windows 11 in the content's very colours, `background` and `text` as
    (red, green, blue), so that bar and window read as one surface (where
    Windows does not know the colour attributes the dark or light bar
    stays); silently ignored elsewhere. And give them the exe's icon at the
    title bar's size."""
    try:
        import ctypes
        from ctypes import wintypes
        user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    except (AttributeError, OSError):
        return
    pid = os.getpid()
    value = ctypes.c_int(1 if dark else 0)
    # DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR (Windows 11): a colour is 0x00BBGGRR
    colours = [(attr, wintypes.DWORD(rgb[0] | rgb[1] << 8 | rgb[2] << 16))
               for attr, rgb in ((35, background), (36, text)) if rgb is not None]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20H1+ / 1809)
                if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                ctypes.sizeof(value)) == 0:
                    break
            for attr, colour in colours:
                dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(colour), ctypes.sizeof(colour))
        return 1

    user32.EnumWindows(visit, 0)
    win_window_icons()
