#!/usr/bin/env python3
"""Compile po/*.po into GNU gettext .mo catalogues (no external tools):

  python tools/compile_po.py                 -> locale/<lang>/LC_MESSAGES/pdf-cropper.mo
  python tools/compile_po.py --dest /app/share/locale

Handles what our catalogues use: plain msgid/msgstr entries, multi-line
strings, C escapes, and the header entry; fuzzy and untranslated entries
are skipped (gettext then falls back to the English source string).
"""

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "pdf-cropper"

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}


def _unquote(text: str) -> str:
    """Decode one quoted po string ("..." with C escapes)."""
    text = text.strip()
    if not (text.startswith('"') and text.endswith('"')):
        raise ValueError(f"malformed string: {text!r}")
    out, i, body = [], 0, text[1:-1]
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_po(path: Path) -> dict:
    """Return {msgid: msgstr} for translated, non-fuzzy entries (plus the header)."""
    entries, current, fuzzy = {}, {}, False
    field = None

    def flush():
        nonlocal current, fuzzy, field
        if "msgid" in current and "msgstr" in current:
            if current["msgstr"] and not fuzzy:
                entries[current["msgid"]] = current["msgstr"]
        current, fuzzy, field = {}, False, None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            continue
        if line.startswith("msgctxt "):
            raise ValueError("msgctxt is not supported by this compiler")
        if line.startswith("msgid_plural") or line.startswith("msgstr["):
            raise ValueError("plural forms are not supported by this compiler")
        for key in ("msgid", "msgstr"):
            if line.startswith(key + " "):
                if key == "msgid" and "msgstr" in current:
                    flush()
                field = key
                current[key] = _unquote(line[len(key):])
                break
        else:
            if line.startswith('"') and field:
                current[field] += _unquote(line)
            else:
                raise ValueError(f"unexpected line in {path.name}: {raw!r}")
    flush()
    return entries


def write_mo(entries: dict, dest: Path) -> None:
    """Write a little-endian .mo file (format documented in the gettext manual)."""
    keys = sorted(entries)
    ids = b"".join(k.encode("utf-8") + b"\0" for k in keys)
    strs = b"".join(entries[k].encode("utf-8") + b"\0" for k in keys)
    n = len(keys)
    header = 7 * 4
    id_table_off = header
    str_table_off = id_table_off + n * 8
    ids_off = str_table_off + n * 8
    strs_off = ids_off + len(ids)
    id_table, str_table = [], []
    pos_i = pos_s = 0
    for k in keys:
        li, ls = len(k.encode("utf-8")), len(entries[k].encode("utf-8"))
        id_table.append(struct.pack("<II", li, ids_off + pos_i))
        str_table.append(struct.pack("<II", ls, strs_off + pos_s))
        pos_i += li + 1
        pos_s += ls + 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(struct.pack("<IIIIIII", 0x950412DE, 0, n, id_table_off, str_table_off, 0, 0))
        f.write(b"".join(id_table))
        f.write(b"".join(str_table))
        f.write(ids)
        f.write(strs)


def compile_all(po_dir: Path, dest: Path) -> list:
    written = []
    for po in sorted(po_dir.glob("*.po")):
        entries = parse_po(po)
        out = dest / po.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
        write_mo(entries, out)
        written.append((po.stem, len(entries) - 1, out))  # minus the header entry
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=ROOT / "locale",
                        help="root of the locale tree to write (default: ./locale)")
    args = parser.parse_args()
    for lang, count, out in compile_all(ROOT / "po", args.dest):
        print(f"{lang}: {count} strings -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
