"""PDF Cropper's own side of the updates: its names in the shared
updates.py (tests/test_updates.py, a copy of the model's, tests the module
itself), the key the release workflow signs with, the command line's
--check-update, the folders of its files."""

import _setup  # noqa: F401  (src on the path, the user's folders guarded)

import contextlib
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import pdf_cropper
from pdf_cropper import cli
from pdf_cropper.app import config, services, updates

ROOT = Path(__file__).resolve().parent.parent


class ProjectTests(unittest.TestCase):
    def test_the_release_files_are_named_as_the_installer_makes_them(self):
        project = services.project()
        self.assertEqual(project.installer_name("v1.3.0"), "pdf-cropper-setup-v1.3.0-windows-x64.exe")
        iss = (ROOT / "installer" / "pdf-cropper.iss").read_text(encoding="utf-8")
        self.assertIn("OutputBaseFilename=pdf-cropper-setup-v{#AppVersion}-windows-x64", iss)
        self.assertTrue(project.is_installer("pdf-cropper-setup-v1.3.0-windows-x64.exe"))
        self.assertFalse(project.is_installer("pdf-cropper-setup-vdev-windows-x64.exe"))      # the CI's builds
        self.assertFalse(project.is_installer("pdf-cropper-v1.3.0-windows-x64-portable.zip"))

    def test_the_project_is_this_one(self):
        project = services.project()
        self.assertEqual(project.downloads, "https://github.com/daniel-g-carrasco/pdf-cropper/releases/download/")
        self.assertEqual(project.version, pdf_cropper.__version__)
        # the same folders as before: an older version's leftovers are cleaned too
        self.assertEqual(project.download_folder_prefix, "pdf-cropper-update-")

    def test_the_release_workflow_signs_with_the_key_the_code_trusts(self):
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        self.assertIn(f"PUBLIC_KEY: {pdf_cropper.PUBLIC_KEY}\n", workflow)
        self.assertEqual(services.project().public_key, pdf_cropper.PUBLIC_KEY)


class CheckUpdateTests(unittest.TestCase):
    """--check-update: 0 when GitHub answered (up to date or not, or with an
    error of its own), 3 when the network or TLS failed before it could."""

    def run_with(self, answer):
        with mock.patch.object(updates.urllib.request, "urlopen", side_effect=answer), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.run_check_update()

    def test_github_answering_an_error_is_reached(self):
        for code in (403, 404, 500):
            error = urllib.error.HTTPError("https://api.github.com", code, "no", {}, io.BytesIO(b""))
            self.assertEqual(self.run_with(error), 0, code)

    def test_the_network_failing_is_not(self):
        self.assertEqual(self.run_with(urllib.error.URLError("no route")), 3)
        self.assertEqual(self.run_with(TimeoutError("timed out")), 3)

    def test_reached(self):
        self.assertTrue(cli.reached(updates.UpdateError("unexpected answer from GitHub")))


@unittest.skipUnless(os.name == "nt", "the folders of Windows")
class FoldersTests(unittest.TestCase):
    def test_the_settings_stay_where_they_have_always_been(self):
        with tempfile.TemporaryDirectory() as home:
            environ = {"LOCALAPPDATA": os.path.join(home, "Local"), "APPDATA": os.path.join(home, "Roaming")}
            paths = services.app_paths(environ)
            self.assertEqual(paths.config, Path(home, "Local", "pdf-cropper"))
            self.assertEqual(paths.config_file, Path(home, "Local", "pdf-cropper", "settings.ini"))
            self.assertEqual(paths.state, Path(home, "Local", "pdf-cropper", "state"))

    def test_the_xdg_variables_win_for_the_tests(self):
        self.assertEqual(config.config_dir(), Path(os.environ["XDG_CONFIG_HOME"]) / "pdf-cropper")


if __name__ == "__main__":
    unittest.main()
