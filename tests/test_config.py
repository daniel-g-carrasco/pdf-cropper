"""PDF Cropper's preferences (app/config.py): saved whole or not at all, a
failure said and not fatal, and what the About window's troubleshooting
text says of them (the shared app/diagnostics.py)."""

import _setup  # noqa: F401  (src on the path, the user's folders guarded)

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pdf_cropper
from pdf_cropper.app import config, diagnostics, services
from pdf_cropper.data import atomic


class SaveTests(unittest.TestCase):
    def test_a_save_that_windows_refuses_leaves_the_file_as_it_was(self):
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "settings.ini"
            settings = config.Settings(ini)
            settings.set("margin_mm", "7.5")
            before = ini.read_bytes()
            stderr = io.StringIO()
            with mock.patch.object(atomic.os, "replace", side_effect=PermissionError("held")), \
                    mock.patch.object(atomic.time, "sleep"), contextlib.redirect_stderr(stderr):
                settings.set("margin_mm", "3")           # said, not raised
            self.assertEqual(ini.read_bytes(), before)
            self.assertEqual(os.listdir(td), ["settings.ini"])     # no temporary file left
            self.assertIn("saving the preferences: PermissionError", stderr.getvalue())
            self.assertEqual(config.Settings(ini).get("margin_mm"), "7.5")

    def test_the_file_reads_back_the_same(self):
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "cfg" / "settings.ini"
            settings = config.Settings(ini)
            settings.set("language", "it")
            settings.set("check_updates", False)
            again = config.Settings(ini)
            self.assertEqual((again.language, again.check_updates, again.theme), ("it", False, "auto"))


class TroubleshootingTests(unittest.TestCase):
    def test_the_text_names_the_settings_file_and_the_errors_file(self):
        paths = services.app_paths()
        settings = config.Settings(paths.config_file)
        text = diagnostics.summary(pdf_cropper.APP_NAME, pdf_cropper.__version__, settings, paths, "GTK 4")
        self.assertIn(f"settings: {paths.config / 'settings.ini'}", text)
        self.assertIn(f"errors: {paths.state / 'errors.log'}", text)
        self.assertIn("check updates True", text)


if __name__ == "__main__":
    unittest.main()
