"""The GTK 4 window: the application, the main window, the dialogs."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from pdf_cropper import APP_ID, APP_NAME, GITHUB_REPO, PACKAGE_DIR, __version__, _mark
from pdf_cropper.app.crop import crop_file, iter_pdf
from pdf_cropper.app.settings import Settings
from pdf_cropper.app.updates import (
    UpdateError,
    clean_update_downloads,
    download_file,
    fetch_latest_release,
    is_installed_build,
    is_newer,
    pick_asset,
    update_download_folder,
)
from pdf_cropper.app.win32 import win_allow_foreground, win_bring_to_front, win_dark_titlebars
from pdf_cropper.app.windows_shell import win_is_registered, win_register, win_unregister
from pdf_cropper.cli import build_parser
from pdf_cropper.core.margins import CropError, MAX_MARGIN_MM, NothingToCrop, clamp_margin, describe
from pdf_cropper.i18n import _

from .winquirks import win_watch_quirks

# GTK 4 GUI

# Plain GTK 4 (no libadwaita): GTK's own named colours.
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


def window_colours(widget):
    """The window's background and text colours as the style has them now,
    each (red, green, blue) 0-255, for Windows' title bar; (None, None)
    when the style does not name them. libadwaita names them window_*,
    plain GTK theme_*; a translucent text colour is given as it looks over
    the background."""
    import warnings
    with warnings.catch_warnings():
        # GTK 4.10 deprecates the style context and gives no other way to
        # read a named colour of the style
        warnings.simplefilter("ignore", DeprecationWarning)
        context = widget.get_style_context()
        for back_name, text_name in (("window_bg_color", "window_fg_color"), ("theme_bg_color", "theme_fg_color")):
            has_back, back = context.lookup_color(back_name)
            has_text, fore = context.lookup_color(text_name)
            if has_back and has_text:
                break
        else:
            return None, None
    base, ink = (back.red, back.green, back.blue), (fore.red, fore.green, fore.blue)
    over = [ink[i] * fore.alpha + base[i] * (1 - fore.alpha) for i in range(3)]
    return tuple(round(c * 255) for c in base), tuple(round(c * 255) for c in over)


def larger_system_font(factor: float = 7 / 6) -> None:
    """Windows: the system font a size larger (Segoe UI 9 -> 10.5 points).
    libadwaita is drawn for an 11-point font, and at 100% the smaller text it
    derives from the system's 9 points (subtitles, captions) is hard to read.
    The factor applies to the size Windows gives, so a larger text size
    chosen in Windows' settings is followed, also when it changes."""
    from gi.repository import Gtk, Pango
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    ours = [""]

    def apply(*_args):
        name = settings.props.gtk_font_name
        if not name or name == ours[0]:
            return
        font = Pango.FontDescription.from_string(name)
        size = font.get_size()
        if size <= 0 or font.get_size_is_absolute():
            return
        half_point = Pango.SCALE // 2
        font.set_size(round(size * factor / half_point) * half_point)
        ours[0] = font.to_string()
        settings.props.gtk_font_name = ours[0]

    apply()
    settings.connect("notify::gtk-font-name", apply)


def run_gui(argv, settings: Settings) -> int:
    is_win = sys.platform == "win32"
    # The Windows title bar (system decorations): GTK honours GTK_CSD only
    # before it initialises, hence the environment variable set up front.
    use_csd = not (is_win and settings.get_bool("system_title_bar"))
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
            "  Windows(MSYS2 UCRT64): pacman -S mingw-w64-ucrt-x86_64-gtk4 "
            "mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject\n"
            + _("or download the portable build from the GitHub Releases.") + "\n"
            + _("Headless use:  pdf-cropper FILE_OR_FOLDER..."),
            file=sys.stderr,
        )
        return 2

    # libadwaita is the GNOME platform library: it supplies the current HIG
    # widgets and follows the system light/dark preference by itself (GTK
    # deprecated gtk-application-prefer-dark-theme in 4.20). Also on Windows,
    # for the content only when the window keeps the system title bar (an
    # AdwApplicationWindow always draws its own, and Snap needs Windows'
    # own: GTK's imitation of it does not work).
    # PDF_CROPPER_NO_ADW=1 forces the plain GTK path (testing aid).
    Adw = None
    if not os.environ.get("PDF_CROPPER_NO_ADW"):
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
        system. With libadwaita that is AdwStyleManager's job, the Windows
        title bar following it; on plain GTK 4 (no libadwaita) the preference
        is read by hand from the personalization key / the freedesktop
        settings portal."""

        def __init__(self):
            self.dark = False
            self._portal = None
            self._poll_id = 0
            self._watching = False
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
                if is_win and not self._watching:
                    # the system title bar follows libadwaita's shade
                    self._watching = True
                    manager.connect("notify::dark", self._on_adw_dark)
                if is_win:
                    self._title_bars()
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

        def _on_adw_dark(self, manager, _pspec):
            self.dark = manager.get_dark()
            self._title_bars()

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
                self._title_bars()

        def _title_bars(self, again=True):
            """Windows: the native title bars as the content is, dark or light,
            and on Windows 11 in the window's own colours; once more a
            moment later, when the style has followed a change of scheme."""
            windows = Gtk.Window.list_toplevels()
            background, text = window_colours(windows[0]) if windows else (None, None)
            win_dark_titlebars(self.dark, background, text)
            if again:
                GLib.idle_add(lambda: self._title_bars(False) and False)

        def watch_window(self, win):
            """Paint the native title bar of a new window in the right shade,
            and work around GTK 4.24's restore and popover quirks."""
            if is_win:
                win.connect("map", lambda _w: self._title_bars(False))
                win_watch_quirks(win)

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

    # The system title bar needs a plain GTK window: libadwaita's always
    # draws its own. Its content is libadwaita's all the same.
    WindowBase = Adw.ApplicationWindow if use_adw and use_csd else Gtk.ApplicationWindow

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
            self._toast = None        # the last one (libadwaita)
            self._about = None
            self.connect("map", lambda _w: _mark("window-mapped"))

            header = Adw.HeaderBar() if use_adw else Gtk.HeaderBar()
            if not use_csd:  # native title bar: the header bar becomes a plain toolbar
                if use_adw:
                    header.set_show_start_title_buttons(False)
                    header.set_show_end_title_buttons(False)
                else:
                    header.set_show_title_buttons(False)
                header.set_title_widget(Gtk.Box())
            elif not use_adw:
                self.set_titlebar(header)    # libadwaita's: placed by the AdwToolbarView below

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
                if use_csd:
                    self.set_content(view)
                else:
                    self.set_child(view)
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
                threading.Thread(target=clean_update_downloads, daemon=True).start()
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
                toast = self._toast = Adw.Toast(title=text, use_markup=False)
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

        def hide_banner(self):
            if use_adw:
                if self._toast is not None:
                    self._toast.dismiss()
            else:
                self.banner.set_reveal_child(False)

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
                    self.hide_banner()
                    UpdateDialog(self, rel).open()
                else:
                    self.show_banner(
                        _("Version {version} of {app} is available.").format(
                            version=rel["version"], app=APP_NAME),
                        _("Update…"), lambda: UpdateDialog(self, rel).open())
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

    class UpdateDialog(Adw.Dialog if use_adw else Gtk.Window):
        def __init__(self, parent, rel):
            if use_adw:
                super().__init__(title=_("Update available"), content_width=480)
                self.connect("closed", self._on_close)
            else:
                super().__init__(transient_for=parent, modal=True, resizable=False,
                                 title=_("Update available"), default_width=480)
                init_dialog(self)
                self.connect("close-request", self._on_close)
            self._parent = parent
            self._app = parent.get_application()
            self._rel = rel
            self._cancel = threading.Event()

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
                                        "version; the app closes and opens again."))
            else:
                self.action = Gtk.Button(label=_("Open the download page"))
                self.action.connect("clicked", self._open_page)
            self.action.add_css_class("suggested-action")
            box.append(button_row(later, self.action))
            if use_adw:
                box.set_margin_top(6)       # right under the header bar
                view = Adw.ToolbarView(content=box)
                view.add_top_bar(Adw.HeaderBar())
                self.set_child(view)
            else:
                self.set_child(box)
            self.set_default_widget(self.action)
            # the notes are selectable: the dialog would open with them
            # focused and all their text selected
            self.set_focus(self.action)

        def open(self):
            if use_adw:
                self.present(self._parent)
            else:
                self.present()

        def _on_close(self, _win):
            self._cancel.set()
            return False

        def _open_page(self, _btn):
            # the launcher wants a GtkWindow: an AdwDialog is not one, its
            # root is (the window it sits in, or its own floating one)
            open_uri(self._rel["url"], self.get_root())
            self.close()

        def _download(self, _btn):
            self.action.set_sensitive(False)
            self.progress.set_visible(True)
            self.progress.set_fraction(0)
            self.status.set_label(_("Downloading…"))

            def progress(done, total):
                GLib.idle_add(self._on_progress, done, total)

            def work():
                try:
                    dest = update_download_folder() / self.asset["name"]
                    download_file(self.asset["url"], dest, progress, self._cancel,
                                  sha256=self.asset.get("sha256", ""), size=self.asset.get("size", 0),
                                  signature_url=self.asset.get("signature_url", ""))
                except Exception as e:  # whatever it is, said: never stuck on "Downloading…"
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
            import subprocess
            self.status.set_label(_("Starting the installer…"))
            try:
                # no wizard: the choices of the first installation are kept,
                # Setup shows its progress and opens the app again
                subprocess.Popen([str(dest), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"],
                                 close_fds=True)
            except OSError as e:
                self.status.set_label(_("Cannot start the installer: {error}").format(error=e))
                self.action.set_sensitive(True)
                return False
            self._app.quit()  # let the installer replace the files in peace
            return False

    # --- preferences --------------------------------------------------------

    # The switch to the system title bar, in either kind of preferences
    TITLE_BAR_HINT = _("Needed to snap the window to the screen edges "
                       "(takes effect at the next start)")

    # File Explorer actions (Windows): what each one tells the user
    def shell_text():
        try:
            registered = win_is_registered()
        except OSError:
            registered = False
        if registered:
            return _("Currently: present (on Windows 11 under “Show more options”)")
        return _("Currently: not present")

    def register():
        try:
            win_register()
            return _("Context-menu entry added.")
        except OSError as e:
            return _("Registration failed: {error}").format(error=e)

    def unregister():
        try:
            win_unregister()
            return _("Context-menu entry removed for the current user.")
        except OSError as e:
            return _("Removal failed: {error}").format(error=e)

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
                    _("Windows title bar"), TITLE_BAR_HINT, self._switch("system_title_bar")))
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
                    shell_text(), reg, return_subtitle=True)
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

        def _refresh(self, message=None):
            self.shell_label.set_label(shell_text())
            if message:
                self._parent.show_banner(message)

        def _on_register(self, _btn):
            self._refresh(register())

        def _on_unregister(self, _btn):
            self._refresh(unregister())

    def adw_preferences(parent):
        """Preferences as an AdwPreferencesDialog: HIG boxed list, combo rows.
        The plain-GTK PreferencesWindow above serves the windows without
        libadwaita."""
        dialog = Adw.PreferencesDialog(title=_("Preferences"))
        schemes = (("auto", _("Automatic (follow the system)")),
                   ("light", _("Light")), ("dark", _("Dark")))
        languages = (("auto", _("Automatic (system language)")),
                     ("it", "Italiano"), ("en", "English"))

        def on_scheme(value):
            settings.set("color_scheme", value)
            theme.apply()

        def tell(message):      # over the dialog, where the user is looking
            dialog.add_toast(Adw.Toast(title=message, use_markup=False))

        def on_language(value):
            settings.set("language", value)
            tell(_("The new language will be used at the next start."))

        def combo(title, subtitle, options, key, callback):
            keys = [k for k, _label in options]
            row = Adw.ComboRow(
                title=title, subtitle=subtitle, use_markup=False,
                model=Gtk.StringList.new([label for _k, label in options]))
            current = settings.get(key)
            row.set_selected(keys.index(current) if current in keys else 0)
            row.connect("notify::selected",
                        lambda r, _p: callback(keys[r.get_selected()]))
            return row

        def switch(title, subtitle, key):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, use_markup=False,
                                active=settings.get_bool(key))
            row.connect("notify::active", lambda r, _p: settings.set(key, r.get_active()))
            return row

        def action_row(title, subtitle, *buttons):
            row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
            for label, callback in buttons:
                button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda _b, c=callback: c(row))
                row.add_suffix(button)
            return row

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("Appearance"))
        group.add(combo(_("Theme"), _("“Automatic” follows the system settings"),
                        schemes, "color_scheme", on_scheme))
        group.add(combo(_("Language"), _("Takes effect at the next start"),
                        languages, "language", on_language))
        if is_win:
            group.add(switch(_("Windows title bar"), TITLE_BAR_HINT, "system_title_bar"))
        page.add(group)

        if is_win:
            group = Adw.PreferencesGroup(title=_("General"))
            group.add(switch(_("Check for updates at start-up"),
                             _("Once a day, from the project's GitHub Releases"),
                             "check_updates"))
            page.add(group)

            def then_refresh(action):
                def run(row):
                    tell(action())
                    row.set_subtitle(shell_text())
                return run

            group = Adw.PreferencesGroup(title=_("File Explorer"))
            group.add(action_row(
                _("“Crop white margins” in the context menu of PDF files"), shell_text(),
                (_("Add"), then_refresh(register)),
                (_("Remove"), then_refresh(unregister))))
            page.add(group)

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
            if is_win:
                larger_system_font()
            _mark("startup")
            display = Gdk.Display.get_default()
            bundle = getattr(sys, "_MEIPASS", None)
            if bundle:  # icons shipped inside the PyInstaller bundle
                Gtk.IconTheme.get_for_display(display).add_search_path(
                    os.path.join(bundle, "share", "icons"))
            else:  # source checkout: data/icons at its root (no-op when installed)
                icons = PACKAGE_DIR.parent.parent / "data" / "icons"
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
