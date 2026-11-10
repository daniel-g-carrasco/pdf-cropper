"""GTK 4.24's window quirks on Windows, worked around per window."""

from __future__ import annotations


def win_watch_quirks(win) -> None:
    """Three quirks of GTK 4.24 on Windows, worked around per window (measured
    2026-11-06 on GTK 4.24.1; nothing here is needed once GTK fixes them).
    A window maximized by Windows (the title bar's button, Win+Up, a drag to
    the top edge) is put back to its normal size by the first popover that
    opens, while GTK and Windows still call it maximized: made GTK's own
    (maximize()), it keeps its size. A restore made by Windows (the title
    bar's button, Win+Down, a double click, the title bar dragged down) after
    a maximize from GTK leaves the window as big as the screen while GTK keeps
    its surface at the maximized size and remembers the old one: from there
    borders drag wrong. Done again through GTK, maximize() then unmaximize(),
    the window gets its size back. Both only once Windows is done with the
    window (not during a move, not with the button held), judged 300 ms later
    on the window's own size (GetWindowRect), and once per restore: a second
    look that still finds it big gives up rather than pulse. And a popover
    open when the window is minimized comes back without its grab (no click
    outside closes it): closed on the resume, it is gone."""
    import ctypes
    from ctypes import wintypes
    from gi.repository import GLib, Gtk
    # our own handles: argtypes set on ctypes.windll.user32 would reach the
    # other users of that shared object with our RECT type
    user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    state = {}      # own: the maximize is GTK's own; size: to put back; fixing: our own maximize + unmaximize

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", RECT)]

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    def hwnd_of():
        try:
            import gi
            gi.require_version("GdkWin32", "4.0")
            from gi.repository import GdkWin32
            return int(GdkWin32.Win32Surface.get_handle(win.get_surface()))
        except Exception:       # no typelib, no method in this GTK: the title is the next best thing
            return int(user32.FindWindowW(None, win.get_title() or "") or 0)

    def busy():
        """A move or a resize in progress (Windows' own loop), or the button still down."""
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if user32.GetGUIThreadInfo(kernel32.GetCurrentThreadId(), ctypes.byref(info)) and info.flags & 0x2:
            return True
        return bool(user32.GetAsyncKeyState(0x01) & 0x8000)

    def oversized(size):
        surface = win.get_surface()
        hwnd, rect = hwnd_of(), RECT()
        if surface is None or not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        scale = surface.get_scale_factor() or 1
        width, height = size
        return rect.right - rect.left > (width + 20) * scale or rect.bottom - rect.top > (height + 20) * scale

    def when_settled(then):
        tries = {"left": 50}                    # at most 5 s

        def look():
            tries["left"] -= 1
            if busy() and tries["left"] > 0:
                return True
            then()
            return False
        GLib.timeout_add(100, look)

    def popovers_below(widget):
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Popover):
                yield child
            yield from popovers_below(child)
            child = child.get_next_sibling()

    def on_maximized(w, _pspec):
        if w.is_maximized():
            if state.get("own") or state.get("fixing"):
                return
            state["own"] = True
            state["size"] = w.get_default_size()

            def make_own():
                if w.is_maximized() and state.get("own") and not state.get("fixing"):
                    w.maximize()
            when_settled(make_own)
            return
        state.pop("own", None)
        if state.get("fixing"):
            return
        size = state.get("size")
        if size is None:
            return

        def judge():
            if w.is_maximized() or state.get("fixing") or not oversized(size):
                return False
            state["fixing"] = True
            w.maximize()

            def back():
                w.unmaximize()
                GLib.timeout_add(100, lambda: (state.pop("fixing", None), False)[1])
                return False
            GLib.timeout_add(60, back)
            return False

        def check():
            if w.is_maximized() or state.get("fixing") or state.get("size") != size:
                return
            state.pop("size", None)             # one try per restore: no pulsing
            GLib.timeout_add(300, judge)
        when_settled(check)

    def on_suspended(w, _pspec):
        if not w.get_property("suspended"):
            def close():
                for popover in popovers_below(w):
                    if popover.get_visible():
                        popover.popdown()
                return False
            GLib.timeout_add(150, close)

    win.connect("notify::maximized", on_maximized)
    win.connect("notify::suspended", on_suspended)
