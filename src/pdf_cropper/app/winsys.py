"""Windows through ctypes, PDF Cropper's own part: the window of the
instance already running brought to the front when a later launch hands it
its files. What every app of mine has from Windows (the title bars, the
windows' icons) is in app/win32.py.
"""

from __future__ import annotations

import os


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
