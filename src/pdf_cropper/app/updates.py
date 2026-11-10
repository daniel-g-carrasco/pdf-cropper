"""A newer version from the GitHub releases: the check, the right file of
a release, its download verified against the release's SHA-256 and the
project's signature.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pdf_cropper import DOWNLOADS_URL, LATEST_RELEASE_API, RELEASES_URL, __version__
from pdf_cropper.i18n import _

from .minisign import minisign_verify


# Used by the Windows builds only: on Linux updates come from the package
# manager / Flatpak (GNOME Software), as the platform conventions require.
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
    """The release asset matching this build: installer or portable zip.
    A released version (never a CI "dev" build), from this project's own
    release downloads; the installer, which is run, only with the SHA-256
    GitHub gives for it and its minisign signature, a file of the release."""
    version = r"v\d+(?:\.\d+){1,3}"
    pattern = (rf"^pdf-cropper-setup-{version}-windows-x64\.exe$" if installed
               else rf"^pdf-cropper-{version}-windows-x64-portable\.zip$")
    for a in assets:
        if (re.match(pattern, a.get("name", ""), re.IGNORECASE)
                and a.get("url", "").startswith(DOWNLOADS_URL)
                and ((a.get("sha256") and a.get("signature_url", "").startswith(DOWNLOADS_URL))
                     or not installed)):
            return a
    return None


UPDATE_FOLDER_PREFIX = "pdf-cropper-update-"


def update_download_folder() -> Path:
    """A new folder of its own for an installer, in the temporary folder."""
    import tempfile
    return Path(tempfile.mkdtemp(prefix=UPDATE_FOLDER_PREFIX))


def clean_update_downloads() -> None:
    """The installers of earlier updates, gone (Setup is done with them by
    the time the app starts again)."""
    import shutil
    import tempfile
    for folder in Path(tempfile.gettempdir()).glob(UPDATE_FOLDER_PREFIX + "*"):
        shutil.rmtree(folder, ignore_errors=True)


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
    """Return {"version", "tag", "url", "notes", "assets": [{"name", "url", "size",
    "sha256", "signature_url"}]}."""
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
    signatures = {a.get("name", ""): a.get("browser_download_url", "") for a in data.get("assets", [])}
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "url": data.get("html_url") or RELEASES_URL,
        "notes": data.get("body") or "",
        "assets": [
            {"name": a.get("name", ""), "url": a.get("browser_download_url", ""),
             "size": int(a.get("size") or 0),
             # GitHub's checksum of the file, "sha256:<hex>"
             "sha256": str(a.get("digest") or "").removeprefix("sha256:")
             if str(a.get("digest") or "").startswith("sha256:") else "",
             # its minisign signature, a file of the release of its own
             "signature_url": signatures.get(a.get("name", "") + ".minisig", "")}
            for a in data.get("assets", [])
        ],
    }


# --- minisign: the signature of the releases ----------------------------------
# The release workflow signs the files of a release with minisign (the key's
# secret half is the repository's MINISIGN_SECRET_KEY), and an update runs
# only an installer signed with it: an installer replaced in the release,
# with its digest, is refused. Verified with the standard library: Ed25519
# from RFC 8032's reference verification, over minisign's prehashed form
# (the BLAKE2b-512 of the file), and the global signature over the trusted
# comment, which names the file.

UPDATE_PUBLIC_KEY = "RWRF+AifJvn/zhz0o7MBObIYHgw9+sWoc4GaBK+qYnJY0AqeJm540y0a"     # key id CEFFF9269F08F845


def download_file(url: str, dest: Path, progress=None, cancelled=None, sha256: str = "", size: int = 0,
                  signature_url: str = "") -> Path:
    """Download url to dest, checked against its size, its SHA-256 and its
    minisign signature (signature_url: made with UPDATE_PUBLIC_KEY, for
    dest's file name): a file that does not match is removed, and without a
    SHA-256 or a signature nothing is downloaded. progress(done, total) is
    called along the way; cancelled is an optional threading.Event."""
    import hashlib
    import urllib.error
    if not sha256:
        raise UpdateError(_("the release gives no checksum for the installer: not downloaded"), reached=True)
    if not signature_url:
        raise UpdateError(_("the release gives no signature for the installer: not downloaded"), reached=True)
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    digest, signed = hashlib.sha256(), hashlib.blake2b(digest_size=64)
    try:
        with _http_get(signature_url, timeout=30) as resp:
            signature = resp.read(4096).decode("utf-8", "replace")
        with _http_get(url, timeout=30) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or size or 0)
            done = 0
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise UpdateError(_("cancelled"), reached=True)
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                signed.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        # a connection cut short ends the reading without an error
        if (size and done != size) or digest.hexdigest().lower() != sha256.lower():
            raise UpdateError(_("the file downloaded does not match the release: not started"), reached=True)
        # signed with this project's key, for this very file: another
        # release's installer, signed too, is not this one
        try:
            trusted = minisign_verify(signed.digest(), signature, UPDATE_PUBLIC_KEY)
        except ValueError:
            trusted = ""
        if f"file:{dest.name}" not in trusted.split():
            raise UpdateError(_("the installer's signature is not valid: not started"), reached=True)
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
