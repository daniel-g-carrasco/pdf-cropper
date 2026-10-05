#!/usr/bin/env python3
"""Write the files that sit next to the published Flatpak repository:

  <pages>/pdf-cropper.flatpakref    one-click install (GNOME Software, KDE Discover)
  <pages>/pdf-cropper.flatpakrepo   adds the remote only
  <pages>/index.html                short landing page with the commands
  <pages>/.nojekyll                 keep GitHub Pages from touching the ostree tree

Usage: flatpak_repo_files.py PAGES_DIR BASE_URL GPG_KEY_BASE64 VERSION
"""

import sys
from pathlib import Path

APP_ID = "io.github.daniel_g_carrasco.pdf-cropper"
TITLE = "PDF Cropper"
HOMEPAGE = "https://github.com/daniel-g-carrasco/pdf-cropper"
COMMENT = "Crop the white margins of PDF pages, many files at once"
DESCRIPTION = (
    "PDF Cropper removes the white margins around the content of PDF pages, "
    "many files at once, and saves the cropped copy next to the original."
)

pages, base, key, version = Path(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4]
repo_url = f"{base}/repo"

(pages / f"pdf-cropper.flatpakref").write_text(f"""[Flatpak Ref]
Name={APP_ID}
Branch=stable
Title={TITLE}
Comment={COMMENT}
Description={DESCRIPTION}
Homepage={HOMEPAGE}
Icon={base}/icon.png
Url={repo_url}
SuggestRemoteName=pdf-cropper
IsRuntime=false
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
GPGKey={key}
""", encoding="utf-8")

(pages / f"pdf-cropper.flatpakrepo").write_text(f"""[Flatpak Repo]
Title={TITLE}
Comment={COMMENT}
Description={DESCRIPTION}
Homepage={HOMEPAGE}
Icon={base}/icon.png
Url={repo_url}
GPGKey={key}
""", encoding="utf-8")

(pages / ".nojekyll").write_text("")

(pages / "index.html").write_text(f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Cropper, repository Flatpak</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; color: #222; }}
  code, pre {{ background: #f3f3f3; border-radius: 6px; padding: .15rem .4rem; }}
  pre {{ padding: .8rem 1rem; overflow-x: auto; }}
  a.btn {{ display: inline-block; background: #1a5fb4; color: #fff; padding: .7rem 1.2rem; border-radius: 8px; text-decoration: none; font-weight: 600; }}
</style>
</head>
<body>
<h1>PDF Cropper, repository Flatpak</h1>
<p>Repository ufficiale dell'app, versione corrente <strong>{version}</strong>. Il runtime GNOME arriva da Flathub.</p>
<p><a class="btn" href="{base}/pdf-cropper.flatpakref">Installa con un clic</a></p>
<p>Oppure da terminale:</p>
<pre>flatpak install {base}/pdf-cropper.flatpakref</pre>
<p>Gli aggiornamenti arrivano da soli, con GNOME Software o con <code>flatpak update</code>.
Il repository è firmato: l'impronta della chiave è pubblicata nel
<a href="{HOMEPAGE}#flatpak">README del progetto</a>.</p>
<p><a href="{HOMEPAGE}">Sorgente e segnalazioni</a></p>
</body>
</html>
""", encoding="utf-8")
print(f"written in {pages}: pdf-cropper.flatpakref, pdf-cropper.flatpakrepo, index.html, .nojekyll (url {repo_url})")
